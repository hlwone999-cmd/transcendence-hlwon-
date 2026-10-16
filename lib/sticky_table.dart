// Kalkulator Lot XAUUSD (IDR) — sticky_table.dart
// Replika tabel web (.tbl): header menempel di atas, kolom pertama menempel
// di kiri saat scroll horizontal, tinggi baris seragam, border bawah per sel,
// lebar kolom fix yang melebar proporsional bila ada ruang sisa, dan
// auto-scroll ke baris pick (terpusat vertikal) seperti scrollTop di web.
//
// PERBAIKAN BUG:
// 1. Header ikut kegeser: header & body dulunya memakai SATU ScrollController
//    di dua scrollable — drag pada salah satunya tidak menggeser yang lain.
//    Sekarang keduanya disinkronkan lewat pasangan controller (mirror
//    proporsional, dua arah), sehingga geser body ikut menggeser header.
// 2. Performa: baris dulunya dibangun SEMUA sekaligus (tabel 1000 baris ×
//    5–12 kolom ≈ 16.000 widget setiap rebuild → berat & patah-patah).
//    Sekarang baris dirender lazy lewat ListView.builder — hanya baris yang
//    terlihat yang dibangun, dengan repaint boundary per baris.
// Catatan: Row di sini TIDAK boleh pakai crossAxisAlignment.stretch — di
// dalam SingleChildScrollView (tinggi tak terbatas) stretch menghasilkan
// constraint h=Infinity dan menggagalkan seluruh frame. Tinggi sel sudah
// ditentukan eksplisit lewat _cellBox.
import 'package:flutter/material.dart';

class TblCol {
  final double width;
  final Widget header;
  const TblCol(this.width, this.header);
}

/// Builder sel lazy: dipanggil hanya untuk baris & kolom yang terlihat.
/// `col` 0 = kolom pertama (menempel kiri), `col` >= 1 = kolom geser.
typedef CellBuilder = Widget Function(BuildContext context, int row, int col);

class StickyTable extends StatefulWidget {
  final List<TblCol> cols;
  final int rowCount;
  final CellBuilder cellBuilder;
  final int? pickRow; // -1 / null = tidak ada
  final double rowHeight;
  final double headerHeight;
  final double maxHeight;
  final Color borderColor;
  final bool autoScrollToPick;
  const StickyTable({
    super.key,
    required this.cols,
    required this.rowCount,
    required this.cellBuilder,
    required this.pickRow,
    required this.borderColor,
    this.rowHeight = 42,
    this.headerHeight = 40,
    this.maxHeight = 640,
    this.autoScrollToPick = true,
  });

  @override
  State<StickyTable> createState() => _StickyTableState();
}

class _StickyTableState extends State<StickyTable> {
  // Pasangan controller vertikal: kolom pertama & kolom geser harus sejajar.
  final ScrollController _vFirst = ScrollController();
  final ScrollController _vRest = ScrollController();
  // Pasangan controller horizontal: header & body harus sejajar.
  final ScrollController _hHead = ScrollController();
  final ScrollController _hBody = ScrollController();
  bool _mirroring = false;
  int? _lastPick;

  @override
  void initState() {
    super.initState();
    _link(_vFirst, _vRest);
    _link(_vRest, _vFirst);
    _link(_hHead, _hBody);
    _link(_hBody, _hHead);
  }

  @override
  void dispose() {
    _vFirst.dispose();
    _vRest.dispose();
    _hHead.dispose();
    _hBody.dispose();
    super.dispose();
  }

  /// Sinkronkan dua arah: geser `a` → `b` mengikuti (proporsional, supaya
  /// tetap presisi bila maxScrollExtent beda sub-piksel).
  void _link(ScrollController a, ScrollController b) {
    a.addListener(() => _mirror(a, b));
  }

  void _mirror(ScrollController from, ScrollController to) {
    if (_mirroring) return;
    if (!from.hasClients || !to.hasClients) return;
    final fp = from.position, tp = to.position;
    if (!fp.hasContentDimensions || !fp.hasPixels) return;
    if (!tp.hasContentDimensions || !tp.hasPixels) return;
    final fMax = fp.maxScrollExtent;
    final tMax = tp.maxScrollExtent;
    final frac = fMax > 0 ? fp.pixels / fMax : 0.0;
    final target = (frac * tMax).clamp(0.0, tMax);
    if ((tp.pixels - target).abs() < 0.5) return;
    _mirroring = true;
    try {
      tp.jumpTo(target);
    } finally {
      _mirroring = false;
    }
  }

  void _scrollToPick() {
    final pick = widget.pickRow;
    if (pick == null || pick < 0 || !widget.autoScrollToPick) return;
    if (!_vRest.hasClients || !_vFirst.hasClients) return;
    final vp = _vRest.position;
    if (!vp.hasContentDimensions || !vp.hasPixels) return;
    final target =
        (pick * widget.rowHeight + widget.rowHeight / 2 - vp.viewportDimension / 2)
            .clamp(0.0, vp.maxScrollExtent);
    _jumpV(_vRest, target);
    _jumpV(_vFirst, target);
  }

  void _jumpV(ScrollController c, double target) {
    if (!c.hasClients) return;
    final p = c.position;
    if (!p.hasContentDimensions || !p.hasPixels) return;
    if ((p.pixels - target).abs() < 0.5) return;
    _mirroring = true;
    try {
      p.jumpTo(target.clamp(0.0, p.maxScrollExtent));
    } finally {
      _mirroring = false;
    }
  }

  Widget _cellBox(Widget child, double width, double height, Color? bg) {
    return Container(
      width: width,
      height: height,
      decoration: BoxDecoration(
        color: bg,
        border: Border(bottom: BorderSide(width: 1, color: widget.borderColor)),
      ),
      child: child,
    );
  }

  @override
  Widget build(BuildContext context) {
    final cols = widget.cols;
    final w0 = cols.first.width;
    final n = widget.rowCount;

    return LayoutBuilder(builder: (context, box) {
      final avail = box.maxWidth;
      final sumRest = cols.sublist(1).fold<double>(0, (a, c) => a + c.width);
      final restW = sumRest < avail - w0 ? avail - w0 : sumRest;
      final scale = sumRest > 0 ? restW / sumRest : 1.0;
      final effW = <double>[
        for (final c in cols.sublist(1)) c.width * scale
      ];

      final contentH = n * widget.rowHeight;
      final bodyH = contentH < widget.maxHeight - widget.headerHeight
          ? contentH
          : widget.maxHeight - widget.headerHeight;

      // ---- header ----
      final headerRest = SizedBox(
        width: restW,
        height: widget.headerHeight,
        child: Row(
          children: [
            for (int j = 1; j < cols.length; j++)
              _cellBox(cols[j].header, effW[j - 1], widget.headerHeight, null),
          ],
        ),
      );

      final table = RepaintBoundary(
        child: Container(
          decoration: BoxDecoration(
            color: Theme.of(context).cardColor,
            borderRadius: BorderRadius.circular(12),
          ),
          clipBehavior: Clip.antiAlias,
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(children: [
                _cellBox(cols[0].header, w0, widget.headerHeight, null),
                Expanded(
                  child: SingleChildScrollView(
                    controller: _hHead,
                    scrollDirection: Axis.horizontal,
                    physics: const ClampingScrollPhysics(),
                    child: headerRest,
                  ),
                ),
              ]),
              SizedBox(
                height: bodyH,
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    // kolom pertama menempel di kiri (lazy)
                    SizedBox(
                      width: w0,
                      child: ListView.builder(
                        controller: _vFirst,
                        itemExtent: widget.rowHeight,
                        itemCount: n,
                        physics: const ClampingScrollPhysics(),
                        itemBuilder: (context, i) => _cellBox(
                            widget.cellBuilder(context, i, 0), w0,
                            widget.rowHeight, null),
                      ),
                    ),
                    // kolom sisanya (lazy), geser horizontal sinkron dgn header
                    Expanded(
                      child: SingleChildScrollView(
                        controller: _hBody,
                        scrollDirection: Axis.horizontal,
                        physics: const ClampingScrollPhysics(),
                        child: SizedBox(
                          width: restW,
                          child: ListView.builder(
                            controller: _vRest,
                            itemExtent: widget.rowHeight,
                            itemCount: n,
                            physics: const ClampingScrollPhysics(),
                            itemBuilder: (context, i) => _cellBox(
                                Row(children: [
                                  for (int j = 1; j < cols.length; j++)
                                    _cellBox(widget.cellBuilder(context, i, j),
                                        effW[j - 1], widget.rowHeight, null),
                                ]),
                                restW,
                                widget.rowHeight,
                                null),
                          ),
                        ),
                      ),
                    ),
                  ],
                ),
              ),
            ],
          ),
        ),
      );

      if (widget.pickRow != _lastPick) {
        _lastPick = widget.pickRow;
        WidgetsBinding.instance.addPostFrameCallback((_) => _scrollToPick());
      }

      return table;
    });
  }
}

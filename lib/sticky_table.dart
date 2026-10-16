// Kalkulator Lot XAUUSD (IDR) — sticky_table.dart
// Replika tabel web (.tbl): header menempel di atas, kolom pertama menempel
// di kiri saat scroll horizontal, tinggi baris seragam, border bawah per sel,
// lebar kolom fix yang melebar proporsional bila ada ruang sisa, dan
// auto-scroll ke baris pick (terpusat vertikal) seperti scrollTop di web.
import 'package:flutter/material.dart';

class TblCol {
  final double width;
  final Widget header;
  const TblCol(this.width, this.header);
}

class StickyTable extends StatefulWidget {
  final List<TblCol> cols;
  /// Sel per baris (sudah berisi styling lengkap), ukuran == cols.length.
  final List<List<Widget>> cells;
  final int? pickRow; // -1 / null = tidak ada
  final double rowHeight;
  final double headerHeight;
  final double maxHeight;
  final Color borderColor;
  final bool autoScrollToPick;
  const StickyTable({
    super.key,
    required this.cols,
    required this.cells,
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
  final ScrollController _v = ScrollController();
  final ScrollController _h = ScrollController();
  int? _lastPick;

  @override
  void dispose() {
    _v.dispose();
    _h.dispose();
    super.dispose();
  }

  void _scrollToPick() {
    final pick = widget.pickRow;
    if (pick == null || pick < 0 || !widget.autoScrollToPick) return;
    if (!_v.hasClients) return;
    final viewport = _v.position.viewportDimension;
    final target =
        (pick * widget.rowHeight + widget.rowHeight / 2 - viewport / 2)
            .clamp(0.0, _v.position.maxScrollExtent);
    _v.jumpTo(target);
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
    final n = widget.cells.length;

    return LayoutBuilder(builder: (context, box) {
      final avail = box.maxWidth;
      final sumRest = cols.sublist(1).fold<double>(0, (a, c) => a + c.width);
      final restW = sumRest < avail - w0 ? avail - w0 : sumRest;
      final scale = restW / sumRest;
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
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            for (int j = 1; j < cols.length; j++)
              _cellBox(cols[j].header, effW[j - 1], widget.headerHeight, null),
          ],
        ),
      );

      // ---- body rows ----
      final firstCol = <Widget>[];
      final restRows = <Widget>[];
      for (final row in widget.cells) {
        firstCol.add(_cellBox(row[0], w0, widget.rowHeight, null));
      }
      for (final row in widget.cells) {
        restRows.add(SizedBox(
          height: widget.rowHeight,
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              for (int j = 1; j < cols.length; j++)
                _cellBox(row[j], effW[j - 1], widget.rowHeight, null),
            ],
          ),
        ));
      }

      final table = Container(
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
                  controller: _h,
                  scrollDirection: Axis.horizontal,
                  child: headerRest,
                ),
              ),
            ]),
            SizedBox(
              height: bodyH,
              child: SingleChildScrollView(
                controller: _v,
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: firstCol),
                    Expanded(
                      child: SingleChildScrollView(
                        controller: _h,
                        scrollDirection: Axis.horizontal,
                        child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: restRows),
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ],
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

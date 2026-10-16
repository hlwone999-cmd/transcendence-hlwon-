// Kalkulator Lot XAUUSD (IDR) — charts.dart
// Port 1:1 dari drawMC() di script.js web: fan chart (perjalanan balance)
// dan histogram (sebaran balance akhir), termasuk margin, warna, dan label.
import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'engine.dart' show shortIdr;
import 'montecarlo.dart';

/// Gambar garis putus-putus (pengganti PathEffect canvas web).
void _dashedLine(Canvas canvas, Offset a, Offset b, List<double> pattern,
    Paint paint) {
  final dx = b.dx - a.dx, dy = b.dy - a.dy;
  final dist = math.sqrt(dx * dx + dy * dy);
  if (dist == 0) return;
  final ux = dx / dist, uy = dy / dist;
  double t = 0;
  int i = 0;
  while (t < dist) {
    final seg = pattern[i % pattern.length];
    final t2 = (t + seg) < dist ? t + seg : dist;
    if (i % 2 == 0) {
      canvas.drawLine(Offset(a.dx + ux * t, a.dy + uy * t),
          Offset(a.dx + ux * t2, a.dy + uy * t2), paint);
    }
    t = t2;
    i++;
  }
}

class ChartColors {
  final Color line, muted, band1, band2, ink, red, gold, good;
  const ChartColors(this.line, this.muted, this.band1, this.band2, this.ink,
      this.red, this.gold, this.good);
}

const TextStyle _lbl = TextStyle(
    fontFamily: 'Instrument Sans', fontSize: 11, height: 1.0, color: Color(0x00000000));

void _text(Canvas canvas, String s, Offset pos, Color color,
    {TextAlign align = TextAlign.left}) {
  final tp = TextPainter(
      text: TextSpan(
          text: s,
          style: _lbl.copyWith(
              color: color,
              // tulis ulang style agar family & size terjaga
              fontFamily: 'Instrument Sans',
              fontSize: 11,
              height: 1.0)),
      textDirection: TextDirection.ltr,
      textAlign: align)
    ..layout();
  Offset o = pos;
  if (align == TextAlign.right) o = Offset(pos.dx - tp.width, pos.dy);
  if (align == TextAlign.center) o = Offset(pos.dx - tp.width / 2, pos.dy);
  tp.paint(canvas, o);
}

/// Padanan blok `g=cv($("mcFan"))` di drawMC().
class FanChart extends StatelessWidget {
  final McResult mc;
  final ChartColors c;
  const FanChart({super.key, required this.mc, required this.c});

  @override
  Widget build(BuildContext context) =>
      CustomPaint(size: const Size(double.infinity, 260), painter: _FanPainter(mc, c));
}

class _FanPainter extends CustomPainter {
  final McResult mc;
  final ChartColors c;
  _FanPainter(this.mc, this.c);

  @override
  void paint(Canvas canvas, Size size) {
    final w = size.width, h = size.height;
    final N = mc.n, bal = mc.bal, dd = mc.dd;
    const L = 58.0, R = 8.0, T = 10.0, B = 22.0;
    final floor = bal * (1 - dd);
    double lo = floor, hi = bal;
    for (final b in mc.bands) {
      if (b[0] < lo) lo = b[0];
      if (b[4] > hi) hi = b[4];
    }
    double pd = (hi - lo) * .05;
    if (pd == 0) pd = 1;
    lo -= pd;
    hi += pd;
    double X(double t) => L + (w - L - R) * t / N;
    double Y(double v) => T + (h - T - B) * (1 - (v - lo) / (hi - lo));

    // grid horizontal + label kiri
    final grid = Paint()
      ..strokeWidth = 1
      ..color = c.line;
    for (int i = 0; i <= 4; i++) {
      final v = lo + (hi - lo) * i / 4;
      final y = Y(v);
      canvas.drawLine(Offset(L, y), Offset(w - R, y), grid);
      _text(canvas, shortIdr(v), Offset(L - 5, y - 5.5), c.muted,
          align: TextAlign.right);
    }
    // label sumbu X
    final step = <int>[1, ((N / 5 / 10).ceil()) * 10].reduce((a, b) => a > b ? a : b);
    for (int t = step; t <= N; t += step) {
      final x = X(t.toDouble());
      if (x > w - 28) break; // mirip min(X(t), w-28) dengan clip agar tak keluar
      _text(canvas, '$t trade', Offset(x, h - 11), c.muted, align: TextAlign.center);
    }
    // band 5–95% lalu 25–75%
    void band(int a, int b, Color col) {
      final path = Path()
        ..moveTo(X(0), Y(mc.bands[0][a]));
      for (int t = 1; t <= N; t++) {
        path.lineTo(X(t.toDouble()), Y(mc.bands[t][a]));
      }
      for (int t = N; t >= 0; t--) {
        path.lineTo(X(t.toDouble()), Y(mc.bands[t][b]));
      }
      path.close();
      canvas.drawPath(path, Paint()..color = col);
    }

    band(0, 4, c.band1);
    band(1, 3, c.band2);
    // garis balance awal
    canvas.drawLine(Offset(L, Y(bal)), Offset(w - R, Y(bal)),
        Paint()..strokeWidth = 1..color = c.ink);
    // batas drawdown (garis putus-putus merah)
    _dashedLine(
        canvas,
        Offset(L, Y(floor)),
        Offset(w - R, Y(floor)),
        const [5, 4],
        Paint()
          ..strokeWidth = 1
          ..color = c.red);
    // median (emas)
    final med = Path()..moveTo(X(0), Y(mc.bands[0][2]));
    for (int t = 1; t <= N; t++) {
      med.lineTo(X(t.toDouble()), Y(mc.bands[t][2]));
    }
    canvas.drawPath(
        med, Paint()..strokeWidth = 2.5..style = PaintingStyle.stroke..color = c.gold);
  }

  @override
  bool shouldRepaint(covariant _FanPainter old) => old.mc != mc || old.c != c;
}

/// Padanan blok `g=cv($("mcHist"))` di drawMC().
class HistChart extends StatelessWidget {
  final McResult mc;
  final ChartColors c;
  const HistChart({super.key, required this.mc, required this.c});

  @override
  Widget build(BuildContext context) =>
      CustomPaint(size: const Size(double.infinity, 200), painter: _HistPainter(mc, c));
}

class _HistPainter extends CustomPainter {
  final McResult mc;
  final ChartColors c;
  _HistPainter(this.mc, this.c);

  @override
  void paint(Canvas canvas, Size size) {
    final w = size.width, h = size.height;
    final bal = mc.bal, S = mc.s, ends = mc.ends;
    const L = 34.0, R = 6.0, T = 8.0, B = 22.0, k = 24;
    final lo = ends[(S * .005).floor()];
    final hi = ends[((S * .995).ceil()) - 1];
    double stp = (hi - lo) / k;
    if (stp == 0) stp = 1;
    final cnt = List<int>.filled(k, 0);
    for (final v in ends) {
      int i = ((v - lo) / stp).floor();
      if (i < 0) i = 0;
      if (i > k - 1) i = k - 1;
      cnt[i]++;
    }
    final mx = cnt.reduce((a, b) => a > b ? a : b);
    final bw = (w - L - R) / k;
    _text(canvas, '${(mx / S * 100).round()}%', Offset(L - 4, T + 3), c.muted,
        align: TextAlign.right);
    for (int i = 0; i < k; i++) {
      final bh = (h - T - B) * cnt[i] / mx;
      final mid = lo + stp * (i + .5);
      canvas.drawRect(
          Rect.fromLTWH(L + i * bw + 1, h - B - bh, bw - 2 < 1 ? 1 : bw - 2, bh),
          Paint()..color = mid >= bal ? c.good : c.red);
    }
    _text(canvas, shortIdr(lo), Offset(L, h - 11), c.muted);
    _text(canvas, shortIdr(hi), Offset(w - R, h - 11), c.muted,
        align: TextAlign.right);
    final bx = L + (w - L - R) * (bal - lo) / (hi - lo);
    if (bx > L && bx < w - R) {
      _dashedLine(
          canvas,
          Offset(bx, T),
          Offset(bx, h - B),
          const [3, 3],
          Paint()
            ..strokeWidth = 1
            ..color = c.ink);
      _text(canvas, 'modal', Offset(bx, h - 11), c.muted, align: TextAlign.center);
    }
  }

  @override
  bool shouldRepaint(covariant _HistPainter old) => old.mc != mc || old.c != c;
}

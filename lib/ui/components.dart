// Transcendence — ui/components.dart
// KOMPONEN UI REUSABLE (redesign total). Murni presentasi: semua nilai,
// teks, dan callback datang dari pemanggil — tidak ada logika bisnis,
// perhitungan, atau akses data di file ini.
import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../ui_kit.dart';

/// Ikon dalam lingkaran / kotak tint lembut.
class IconBubble extends StatelessWidget {
  final IconData icon;
  final Color color;
  final double size;
  final bool square;
  const IconBubble(
    this.icon, {
    super.key,
    required this.color,
    this.size = 36,
    this.square = false,
  });

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    return Container(
      width: size,
      height: size,
      decoration: BoxDecoration(
        color: color.withAlpha(p.isDark ? 40 : 28),
        shape: square ? BoxShape.rectangle : BoxShape.circle,
        borderRadius: square ? BorderRadius.circular(size * 0.32) : null,
      ),
      alignment: Alignment.center,
      child: Icon(icon, size: size * 0.48, color: color),
    );
  }
}

/// Dekorasi kartu standar (dipakai juga oleh widget yang butuh Container
/// sendiri, mis. kartu lipat).
BoxDecoration cardDecoration(Palette p, {Color? color, double radius = 22}) =>
    BoxDecoration(
      color: color ?? p.card,
      borderRadius: BorderRadius.circular(radius),
      border: Border.all(width: 1, color: p.isDark ? p.lineSoft : p.line),
      boxShadow: p.cardShadow,
    );

/// Kartu seksi gaya referensi: judul tebal + subjudul abu di kiri atas,
/// aksi opsional di kanan, lalu konten.
class SectionCard extends StatelessWidget {
  final String title;
  final String? subtitle;
  final IconData? icon;
  final Widget? trailing;
  final List<Widget> children;
  final EdgeInsets padding;
  const SectionCard({
    super.key,
    required this.title,
    this.subtitle,
    this.icon,
    this.trailing,
    required this.children,
    this.padding = const EdgeInsets.fromLTRB(18, 18, 18, 18),
  });

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    return Container(
      width: double.infinity,
      padding: padding,
      decoration: cardDecoration(p),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              if (icon != null) ...[
                IconBubble(icon!, color: p.gold, size: 34, square: true),
                const SizedBox(width: 12),
              ],
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      title,
                      style: txt(context, 16, w: FontWeight.w600, ls: -0.2),
                    ),
                    if (subtitle != null) ...[
                      const SizedBox(height: 2),
                      Text(
                        subtitle!,
                        style: txt(context, 12, color: p.muted, height: 1.4),
                      ),
                    ],
                  ],
                ),
              ),
              if (trailing != null) ...[const SizedBox(width: 10), trailing!],
            ],
          ),
          const SizedBox(height: 14),
          ...children,
        ],
      ),
    );
  }
}

/// Kartu statistik gaya referensi ("Net Income $193.000 ↗ +35%").
class StatCard extends StatelessWidget {
  final String label;
  final String value;
  final Color valueColor;
  final IconData icon;
  final Color iconColor;
  final String? note;
  final IconData? noteIcon;
  final Color? noteColor;
  const StatCard({
    super.key,
    required this.label,
    required this.value,
    required this.valueColor,
    required this.icon,
    required this.iconColor,
    this.note,
    this.noteIcon,
    this.noteColor,
  });

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    return Container(
      padding: const EdgeInsets.fromLTRB(16, 16, 14, 16),
      decoration: cardDecoration(p, radius: 22),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  label,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: txt(context, 12.5, w: FontWeight.w500, color: p.muted),
                ),
              ),
              IconBubble(icon, color: iconColor, size: 28),
            ],
          ),
          const SizedBox(height: 12),
          FittedBox(
            fit: BoxFit.scaleDown,
            alignment: Alignment.centerLeft,
            child: Text(
              value,
              maxLines: 1,
              style: mono(
                context,
                22,
                w: FontWeight.w600,
                color: valueColor,
                ls: -0.6,
              ),
            ),
          ),
          if (note != null) ...[
            const SizedBox(height: 6),
            Row(
              children: [
                if (noteIcon != null) ...[
                  Icon(noteIcon, size: 14, color: noteColor ?? p.muted),
                  const SizedBox(width: 4),
                ],
                Expanded(
                  child: Text(
                    note!,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: txt(context, 11.5, color: p.muted),
                  ),
                ),
              ],
            ),
          ],
        ],
      ),
    );
  }
}

/// Kartu hero bergradien biru dengan lingkaran dekoratif.
class HeroCard extends StatelessWidget {
  final Widget child;
  final EdgeInsets padding;
  const HeroCard({
    super.key,
    required this.child,
    this.padding = const EdgeInsets.all(20),
  });

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    Widget orb(double s) => IgnorePointer(
      child: Container(
        width: s,
        height: s,
        decoration: BoxDecoration(
          shape: BoxShape.circle,
          color: p.onHero.withAlpha(p.isDark ? 16 : 24),
        ),
      ),
    );
    return Container(
      width: double.infinity,
      decoration: BoxDecoration(
        gradient: p.heroGradient,
        borderRadius: BorderRadius.circular(26),
        boxShadow: [
          BoxShadow(
            color: p.goldSolid.withAlpha(p.isDark ? 40 : 70),
            blurRadius: 26,
            spreadRadius: -10,
            offset: const Offset(0, 10),
          ),
        ],
      ),
      clipBehavior: Clip.antiAlias,
      child: Stack(
        children: [
          Positioned(right: -64, top: -76, child: orb(180)),
          Positioned(left: 90, bottom: -110, child: orb(160)),
          Padding(padding: padding, child: child),
        ],
      ),
    );
  }
}

/// Chip kecil transparan di atas hero ("Update" pada referensi).
class HeroChip extends StatelessWidget {
  final String text;
  final IconData? icon;
  const HeroChip(this.text, {super.key, this.icon});

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
      decoration: BoxDecoration(
        color: p.onHero.withAlpha(38),
        borderRadius: BorderRadius.circular(999),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          if (icon != null) ...[
            Icon(icon, size: 13, color: p.onHero),
            const SizedBox(width: 5),
          ],
          Flexible(
            child: Text(
              text,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(
                fontFamily: kBodyFont,
                fontSize: 11,
                fontWeight: FontWeight.w600,
                letterSpacing: 0.6,
                color: p.onHero,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

/// Tampilan status kosong / error / memuat yang seragam.
class StateView extends StatelessWidget {
  final IconData icon;
  final String title;
  final String? message;
  final Color? color;
  final Widget? action;
  final bool loading;
  const StateView({
    super.key,
    required this.icon,
    required this.title,
    this.message,
    this.color,
    this.action,
    this.loading = false,
  });

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    final c = color ?? p.gold;
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.fromLTRB(20, 24, 20, 22),
      decoration: cardDecoration(p),
      child: Column(
        children: [
          if (loading)
            SizedBox(
              width: 44,
              height: 44,
              child: CircularProgressIndicator(strokeWidth: 3, color: c),
            )
          else
            IconBubble(icon, color: c, size: 52),
          const SizedBox(height: 14),
          Text(
            title,
            textAlign: TextAlign.center,
            style: txt(context, 15, w: FontWeight.w600),
          ),
          if (message != null) ...[
            const SizedBox(height: 6),
            Text(
              message!,
              textAlign: TextAlign.center,
              style: txt(context, 12.5, color: p.muted, height: 1.5),
            ),
          ],
          if (action != null) ...[const SizedBox(height: 16), action!],
        ],
      ),
    );
  }
}

/// Satu segmen donut.
class DonutSegment {
  final double value;
  final Color color;
  final String label;
  const DonutSegment(this.value, this.color, this.label);
}

/// Donut chart bersegmen (gaya "Total View Performance" pada referensi).
/// Hanya menggambar nilai yang diberikan — tidak menghitung apa pun.
class SegmentDonut extends StatelessWidget {
  final List<DonutSegment> segments;
  final double size;
  final Widget center;
  const SegmentDonut({
    super.key,
    required this.segments,
    required this.center,
    this.size = 168,
  });

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    return SizedBox(
      width: size,
      height: size,
      child: Stack(
        alignment: Alignment.center,
        children: [
          CustomPaint(
            size: Size.square(size),
            painter: _SegDonutPainter(segments, p.cardHighest),
          ),
          center,
        ],
      ),
    );
  }
}

class _SegDonutPainter extends CustomPainter {
  final List<DonutSegment> segs;
  final Color track;
  _SegDonutPainter(this.segs, this.track);

  @override
  void paint(Canvas canvas, Size size) {
    final stroke = size.width * 0.15;
    final rect = Rect.fromLTWH(
      stroke / 2,
      stroke / 2,
      size.width - stroke,
      size.height - stroke,
    );
    final paint = Paint()
      ..style = PaintingStyle.stroke
      ..strokeWidth = stroke
      ..strokeCap = StrokeCap.butt;
    final total = segs.fold<double>(
      0,
      (a, s) => a + (s.value > 0 ? s.value : 0),
    );
    if (total <= 0) {
      canvas.drawArc(rect, 0, math.pi * 2, false, paint..color = track);
      return;
    }
    const gap = 0.05;
    var start = -math.pi / 2;
    final nonZero = segs.where((s) => s.value > 0).length;
    for (final s in segs) {
      if (s.value <= 0) continue;
      final sweep = s.value / total * math.pi * 2;
      final g = nonZero > 1 ? gap : 0.0;
      canvas.drawArc(
        rect,
        start + g / 2,
        math.max(0.001, sweep - g),
        false,
        paint..color = s.color,
      );
      start += sweep;
    }
  }

  @override
  bool shouldRepaint(_SegDonutPainter o) => o.segs != segs || o.track != track;
}

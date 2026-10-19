// Transcendence — ui_kit.dart
// Sistem desain "Obsidian Precision" (dari stitch_transcendence_minimalist_redesign).
// Hanya lapisan visual: palet, tipografi, dan komponen dasar.
// ALGORITMA TIDAK ADA DI FILE INI.
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

// ---------------------------------------------------------------------------
// Palet Obsidian Precision (strict dark mode)
// ---------------------------------------------------------------------------

class Palette {
  // Arsitektur netral
  final Color canvas; // latar root #0B0F17
  final Color surface1; // panel #111827
  final Color card; // kartu #1A2234
  final Color cardHi; // kartu aktif/hover #232D42
  final Color cardHighest; // #2E3545
  final Color track; // track segmented paling gelap #0E1420
  final Color line; // border #1F293D
  final Color lineSoft; // border lembut #1E293B
  final Color rim; // rim highlight #334155

  // Teks
  final Color ink; // teks utama #F8FAFC
  final Color muted; // teks sekunder #94A3B8
  final Color dim; // teks lebih redup #64748B
  final Color warm; // label hangat #D8C3AD

  // Aksen fungsional
  final Color gold; // teks emas #FFC174
  final Color goldSolid; // tombol emas #F59E0B
  final Color onGold; // teks di atas emas #2A1700
  final Color goldsoft; // tint emas (bg baris pick / pill)
  final Color mint; // teks mint #4EDEA3
  final Color mintSolid; // #10B981
  final Color good; // alias mint
  final Color goodsoft; // tint mint
  final Color red; // teks salmon lembut #FF938C
  final Color redSolid; // crimson #EF4444
  final Color redsoft; // tint merah

  // Band chart
  final Color band1; // 5–95% (mint transparan)
  final Color band2; // 25–75% (mint lebih pekat)

  const Palette({
    required this.canvas,
    required this.surface1,
    required this.card,
    required this.cardHi,
    required this.cardHighest,
    required this.track,
    required this.line,
    required this.lineSoft,
    required this.rim,
    required this.ink,
    required this.muted,
    required this.dim,
    required this.warm,
    required this.gold,
    required this.goldSolid,
    required this.onGold,
    required this.goldsoft,
    required this.mint,
    required this.mintSolid,
    required this.good,
    required this.goodsoft,
    required this.red,
    required this.redSolid,
    required this.redsoft,
    required this.band1,
    required this.band2,
  });

  // Nama lama yang masih dipakai beberapa file — petakan ke token baru.
  Color get paper => canvas;
  Color get panel => card;

  static const dark = Palette(
    canvas: Color(0xFF0B0F17),
    surface1: Color(0xFF111827),
    card: Color(0xFF1A2234),
    cardHi: Color(0xFF232D42),
    cardHighest: Color(0xFF2E3545),
    track: Color(0xFF0E1420),
    line: Color(0xFF1F293D),
    lineSoft: Color(0xFF1E293B),
    rim: Color(0xFF334155),
    ink: Color(0xFFF8FAFC),
    muted: Color(0xFF94A3B8),
    dim: Color(0xFF64748B),
    warm: Color(0xFFD8C3AD),
    gold: Color(0xFFFFC174),
    goldSolid: Color(0xFFF59E0B),
    onGold: Color(0xFF2A1700),
    goldsoft: Color(0x26F59E0B),
    mint: Color(0xFF4EDEA3),
    mintSolid: Color(0xFF10B981),
    good: Color(0xFF4EDEA3),
    goodsoft: Color(0x2910B981),
    red: Color(0xFFFF938C),
    redSolid: Color(0xFFEF4444),
    redsoft: Color(0x1FEF4444),
    band1: Color(0x2E10B981),
    band2: Color(0x5210B981),
  );

  static Palette of(BuildContext c) => dark;
}

// ---------------------------------------------------------------------------
// Tipografi — Geist (naratif) + JetBrains Mono (angka)
// ---------------------------------------------------------------------------

const String kBodyFont = 'Geist';
const String kHeadFont = 'Geist';
const String kMonoFont = 'JetBrains Mono';

const tabular = [FontFeature.tabularFigures()];

TextStyle txt(BuildContext c, double size,
    {FontWeight w = FontWeight.w400, Color? color, double? height, double? ls}) {
  final p = Palette.of(c);
  return TextStyle(
      fontFamily: kBodyFont,
      fontSize: size,
      fontWeight: w,
      height: height ?? 1.35,
      letterSpacing: ls,
      color: color ?? p.ink);
}

/// Label kecil huruf besar (label-sm uppercase tracking-wider).
TextStyle labelSm(BuildContext c, {Color? color}) => TextStyle(
    fontFamily: kBodyFont,
    fontSize: 10.5,
    fontWeight: FontWeight.w500,
    letterSpacing: 1.4,
    height: 1.3,
    color: color ?? Palette.of(c).muted);

/// Angka mono (label-md mono).
TextStyle mono(BuildContext c, double size,
    {FontWeight w = FontWeight.w500, Color? color, double? ls}) {
  final p = Palette.of(c);
  return TextStyle(
      fontFamily: kMonoFont,
      fontSize: size,
      fontWeight: w,
      letterSpacing: ls ?? 0.4,
      height: 1.3,
      color: color ?? p.ink,
      fontFeatures: tabular);
}

double clampSize(double v, double lo, double hi) =>
    v < lo ? lo : (v > hi ? hi : v);

// ---------------------------------------------------------------------------
// Komponen dasar
// ---------------------------------------------------------------------------

/// Kartu obsidian: radius 16, bg card, border lineSoft.
class ObsCard extends StatelessWidget {
  final List<Widget> children;
  final EdgeInsets padding;
  final Color? color;
  final BorderRadius? radius;
  const ObsCard({
    super.key,
    required this.children,
    this.padding = const EdgeInsets.all(16),
    this.color,
    this.radius,
  });

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    return Container(
      padding: padding,
      decoration: BoxDecoration(
        color: color ?? p.card,
        border: Border.all(width: 1, color: p.lineSoft),
        borderRadius: radius ?? BorderRadius.circular(16),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: children,
      ),
    );
  }
}

/// Kilau blur dekoratif di sudut kartu (bg-primary/10 blur-2xl).
class CornerGlow extends StatelessWidget {
  final Color color;
  final double size;
  final Alignment alignment;
  const CornerGlow(
      {super.key,
      required this.color,
      this.size = 128,
      this.alignment = const Alignment(1.15, -1.15)});

  @override
  Widget build(BuildContext context) {
    return IgnorePointer(
      child: Align(
        alignment: alignment,
        child: Container(
          width: size,
          height: size,
          decoration: BoxDecoration(
            shape: BoxShape.circle,
            boxShadow: [
              BoxShadow(color: color, blurRadius: 48, spreadRadius: 24),
            ],
          ),
        ),
      ),
    );
  }
}

/// Badge pill kecil.
class BadgePill extends StatelessWidget {
  final String text;
  final Color color; // warna teks
  final Color? bg;
  final IconData? icon;
  const BadgePill(this.text, {super.key, required this.color, this.bg, this.icon});

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
      decoration: BoxDecoration(
        color: bg ?? p.cardHi,
        borderRadius: BorderRadius.circular(6),
      ),
      child: Row(mainAxisSize: MainAxisSize.min, children: [
        if (icon != null) ...[
          Icon(icon, size: 12, color: color),
          const SizedBox(width: 4),
        ],
        Flexible(
          child: Text(text,
              maxLines: 1, overflow: TextOverflow.ellipsis,
              style: TextStyle(
                  fontFamily: kBodyFont,
                  fontSize: 10,
                  fontWeight: FontWeight.w600,
                  letterSpacing: 0.8,
                  color: color)),
        ),
      ]),
    );
  }
}

/// Tombol utama emas solid (Calculate / Execute).
class GoldButton extends StatelessWidget {
  final String text;
  final IconData? icon;
  final VoidCallback? onTap;
  final double height;
  const GoldButton(this.text,
      {super.key, this.icon, this.onTap, this.height = 48});

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    return Material(
      color: p.goldSolid,
      borderRadius: BorderRadius.circular(10),
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(10),
        child: SizedBox(
          height: height,
          child: Row(mainAxisAlignment: MainAxisAlignment.center, children: [
            if (icon != null) ...[
              Icon(icon, size: 18, color: p.onGold),
              const SizedBox(width: 8),
            ],
            Flexible(
              child: Text(text,
                  maxLines: 1, overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                      fontFamily: kMonoFont,
                      fontSize: 14,
                      fontWeight: FontWeight.w600,
                      letterSpacing: 0.3,
                      color: p.onGold)),
            ),
          ]),
        ),
      ),
    );
  }
}

/// Tombol sekunder: bg card, border, teks terang.
class GhostButton extends StatelessWidget {
  final String text;
  final IconData? icon;
  final VoidCallback? onTap;
  final Color? textColor;
  final bool expanded;
  const GhostButton(this.text,
      {super.key, this.icon, this.onTap, this.textColor, this.expanded = false});

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    final btn = Material(
      color: p.card,
      borderRadius: BorderRadius.circular(8),
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(8),
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(8),
            border: Border.all(width: 1, color: p.line),
          ),
          child: Row(mainAxisSize: MainAxisSize.min, children: [
            if (icon != null) ...[
              Icon(icon, size: 15, color: textColor ?? p.gold),
              const SizedBox(width: 6),
            ],
            // Flexible: teks panjang (tanggal panjang, label dinamis) tidak
            // boleh bikin Row overflow di layar sempit.
            Flexible(
              child: Text(text,
                  maxLines: 1, overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                      fontFamily: kMonoFont,
                      fontSize: 12,
                      fontWeight: FontWeight.w500,
                      letterSpacing: 0.4,
                      color: textColor ?? p.ink)),
            ),
          ]),
        ),
      ),
    );
    return expanded ? SizedBox(width: double.infinity, child: btn) : btn;
  }
}

/// Chip preset (15/20/25/30/40).
class PresetChip extends StatelessWidget {
  final String text;
  final bool active;
  final VoidCallback onTap;
  const PresetChip(this.text,
      {super.key, required this.active, required this.onTap});

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    return Expanded(
      child: Material(
        color: active ? p.goldSolid : p.cardHi,
        borderRadius: BorderRadius.circular(8),
        child: InkWell(
          onTap: onTap,
          borderRadius: BorderRadius.circular(8),
          child: Container(
            padding: const EdgeInsets.symmetric(vertical: 9),
            alignment: Alignment.center,
            child: Text(text,
                style: TextStyle(
                    fontFamily: kMonoFont,
                    fontSize: 12,
                    fontWeight: active ? FontWeight.w600 : FontWeight.w500,
                    color: active ? p.onGold : p.ink)),
          ),
        ),
      ),
    );
  }
}

/// Baris toggle gaya iOS (Mode Recovery / Compounding).
class ToggleRow extends StatelessWidget {
  final String title;
  final String subtitle;
  final bool value;
  final ValueChanged<bool> onChanged;
  final bool enabled;
  const ToggleRow({
    super.key,
    required this.title,
    required this.subtitle,
    required this.value,
    required this.onChanged,
    this.enabled = true,
  });

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    return Row(children: [
      Expanded(
        child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
          Text(title, style: txt(context, 14, w: FontWeight.w500)),
          const SizedBox(height: 2),
          Text(subtitle, style: txt(context, 12, color: p.muted)),
        ]),
      ),
      const SizedBox(width: 12),
      Switch.adaptive(
        value: value,
        onChanged: enabled ? onChanged : null,
        activeThumbColor: p.goldSolid,
        activeTrackColor: p.goldsoft,
        inactiveThumbColor: p.muted,
        inactiveTrackColor: p.cardHighest,
      ),
    ]);
  }
}

/// Field input obsidian: label di atas, prefix kiri opsional, bg surface1.
class LabeledField extends StatelessWidget {
  final String label;
  final TextEditingController controller;
  final ValueChanged<String> onChanged;
  final String? prefix;
  final Widget? suffix;
  final bool big; // gaya input utama (headline)
  final String? hint;
  final Color? hintColor;
  final Widget? labelTrailing;
  const LabeledField({
    super.key,
    required this.label,
    required this.controller,
    required this.onChanged,
    this.prefix,
    this.suffix,
    this.big = false,
    this.hint,
    this.hintColor,
    this.labelTrailing,
  });

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(mainAxisAlignment: MainAxisAlignment.spaceBetween, children: [
          Text(label, style: labelSm(context)),
          ?labelTrailing,
        ]),
        const SizedBox(height: 6),
        Container(
          decoration: BoxDecoration(
            color: p.surface1,
            borderRadius: BorderRadius.circular(10),
            border: Border.all(width: 1, color: p.line),
          ),
          padding: EdgeInsets.symmetric(
              horizontal: 12, vertical: big ? 10 : 8),
          child: Row(children: [
            if (prefix != null) ...[
              Text(prefix!,
                  style: mono(context, big ? 18 : 13,
                      w: FontWeight.w500, color: p.muted)),
              const SizedBox(width: 8),
            ],
            Expanded(
              child: TextField(
                controller: controller,
                onChanged: onChanged,
                keyboardType:
                    const TextInputType.numberWithOptions(decimal: true),
                inputFormatters: [
                  FilteringTextInputFormatter.allow(
                      RegExp(r'[0-9.,jtrbkmiuaJTRBKMIUA\s]'))
                ],
                style: big
                    ? mono(context, 26, w: FontWeight.w600, ls: -0.3)
                    : mono(context, 15, w: FontWeight.w600),
                decoration: const InputDecoration(
                    isDense: true,
                    filled: false,
                    border: InputBorder.none,
                    contentPadding: EdgeInsets.zero),
                cursorColor: p.goldSolid,
              ),
            ),
            ?suffix,
          ]),
        ),
        if (hint != null) ...[
          const SizedBox(height: 6),
          Text(hint!,
              style: txt(context, 12,
                  color: hintColor ?? p.muted, height: 1.45)),
        ],
      ],
    );
  }
}

/// Judul seksi kecil dengan ikon emas.
class SectionHead extends StatelessWidget {
  final IconData icon;
  final String text;
  final Widget? trailing;
  const SectionHead(this.icon, this.text, {super.key, this.trailing});

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    return Row(children: [
      Icon(icon, size: 17, color: p.gold),
      const SizedBox(width: 8),
      Expanded(
        child: Text(text,
            style: txt(context, 15, w: FontWeight.w600)),
      ),
      ?trailing,
    ]);
  }
}

/// Titik status berdenyut (dot indikator).
class PulseDot extends StatelessWidget {
  final Color color;
  final double size;
  const PulseDot(this.color, {super.key, this.size = 7});

  @override
  Widget build(BuildContext context) {
    return Container(
      width: size,
      height: size,
      decoration: BoxDecoration(
          shape: BoxShape.circle,
          color: color,
          boxShadow: [BoxShadow(color: color.withAlpha(90), blurRadius: 6)]),
    );
  }
}

/// Angka int bergaya id-ID: "1.234.567" (tanpa Rp) — untuk sel tabel.
String jsNum(double x) =>
    x == x.roundToDouble() ? x.toInt().toString() : x.toString();

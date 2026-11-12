// Transcendence — ui_kit.dart
// Sistem desain (redesign total hitam-biru / putih-biru).
// Hanya lapisan visual: tipografi + komponen dasar. Token warna ada di
// ui/tokens.dart (di-export ulang di sini agar import lama tetap berlaku).
// ALGORITMA TIDAK ADA DI FILE INI.
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'ui/components.dart';
import 'ui/tokens.dart';

export 'ui/tokens.dart';
export 'ui/components.dart';
export 'ui/navigation.dart';

// ---------------------------------------------------------------------------
// Tipografi — Geist (naratif) + JetBrains Mono (angka)
// ---------------------------------------------------------------------------

const String kBodyFont = 'Geist';
const String kHeadFont = 'Geist';
const String kMonoFont = 'JetBrains Mono';

const tabular = [FontFeature.tabularFigures()];

TextStyle txt(
  BuildContext c,
  double size, {
  FontWeight w = FontWeight.w400,
  Color? color,
  double? height,
  double? ls,
}) {
  final p = Palette.of(c);
  return TextStyle(
    fontFamily: kBodyFont,
    fontSize: size,
    fontWeight: w,
    height: height ?? 1.35,
    letterSpacing: ls,
    color: color ?? p.ink,
  );
}

/// Label kecil huruf besar (label-sm uppercase tracking-wider).
/// Phase 1.1 Temuan 7: dinaikkan dari 10.5px → 11.5px (tracking 1.4 → 1.2)
/// — 10.5px dengan tracking lebar nyaris tak terbaca di layar ponsel.
TextStyle labelSm(BuildContext c, {Color? color}) => TextStyle(
  fontFamily: kBodyFont,
  fontSize: 11.5,
  fontWeight: FontWeight.w500,
  letterSpacing: 1.2,
  height: 1.3,
  color: color ?? Palette.of(c).muted,
);

/// Angka mono (label-md mono).
TextStyle mono(
  BuildContext c,
  double size, {
  FontWeight w = FontWeight.w500,
  Color? color,
  double? ls,
}) {
  final p = Palette.of(c);
  return TextStyle(
    fontFamily: kMonoFont,
    fontSize: size,
    fontWeight: w,
    letterSpacing: ls ?? 0.4,
    height: 1.3,
    color: color ?? p.ink,
    fontFeatures: tabular,
  );
}

/// Warna teks yang tetap terbaca di atas latar warna aksen [bg]
/// (mis. teks di atas tombol/pill solid emas, mint, atau merah).
Color onColor(BuildContext c, Color bg) =>
    bg.computeLuminance() > 0.4 ? const Color(0xFF0D1321) : Colors.white;

double clampSize(double v, double lo, double hi) =>
    v < lo ? lo : (v > hi ? hi : v);

// ---------------------------------------------------------------------------
// Komponen dasar
// ---------------------------------------------------------------------------

/// Kartu dasar: radius 20, bg card, border halus (+ bayangan lembut di light).
class ObsCard extends StatelessWidget {
  final List<Widget> children;
  final EdgeInsets padding;
  final Color? color;
  final BorderRadius? radius;
  const ObsCard({
    super.key,
    required this.children,
    this.padding = const EdgeInsets.all(18),
    this.color,
    this.radius,
  });

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    return Container(
      padding: padding,
      decoration: cardDecoration(p, color: color).copyWith(
        borderRadius: radius ?? BorderRadius.circular(22),
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
  const CornerGlow({
    super.key,
    required this.color,
    this.size = 128,
    this.alignment = const Alignment(1.15, -1.15),
  });

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
  const BadgePill(
    this.text, {
    super.key,
    required this.color,
    this.bg,
    this.icon,
  });

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 4),
      decoration: BoxDecoration(
        color: bg ?? p.cardHi,
        borderRadius: BorderRadius.circular(999),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          if (icon != null) ...[
            Icon(icon, size: 12, color: color),
            const SizedBox(width: 4),
          ],
          Flexible(
            child: Text(
              text,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(
                fontFamily: kBodyFont,
                fontSize: 10,
                fontWeight: FontWeight.w600,
                letterSpacing: 0.8,
                color: color,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

/// Tombol utama — isian biru primer, bentuk pill (nama kelas dipertahankan).
class GoldButton extends StatelessWidget {
  final String text;
  final IconData? icon;
  final VoidCallback? onTap;
  final double height;
  const GoldButton(
    this.text, {
    super.key,
    this.icon,
    this.onTap,
    this.height = 52,
  });

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    return DecoratedBox(
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(999),
        boxShadow: [
          BoxShadow(
            color: p.goldSolid.withAlpha(p.isDark ? 70 : 55),
            blurRadius: 18,
            spreadRadius: -6,
            offset: const Offset(0, 8),
          ),
        ],
      ),
      child: Material(
        color: p.goldSolid,
        borderRadius: BorderRadius.circular(999),
        child: InkWell(
          onTap: onTap,
          borderRadius: BorderRadius.circular(999),
          child: SizedBox(
            height: height,
            child: Row(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                if (icon != null) ...[
                  Icon(icon, size: 18, color: p.onGold),
                  const SizedBox(width: 8),
                ],
                Flexible(
                  child: Text(
                    text,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(
                      fontFamily: kBodyFont,
                      fontSize: 14,
                      fontWeight: FontWeight.w600,
                      letterSpacing: 0.4,
                      color: p.onGold,
                    ),
                  ),
                ),
              ],
            ),
          ),
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
  const GhostButton(
    this.text, {
    super.key,
    this.icon,
    this.onTap,
    this.textColor,
    this.expanded = false,
  });

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    final btn = Material(
      color: p.isDark ? p.cardHi : p.card,
      borderRadius: BorderRadius.circular(999),
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(999),
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 11),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(999),
            border: Border.all(width: 1, color: p.line),
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              if (icon != null) ...[
                Icon(icon, size: 15, color: textColor ?? p.gold),
                const SizedBox(width: 6),
              ],
              // Flexible: teks panjang (tanggal panjang, label dinamis) tidak
              // boleh bikin Row overflow di layar sempit.
              Flexible(
                child: Text(
                  text,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                    fontFamily: kBodyFont,
                    fontSize: 12.5,
                    fontWeight: FontWeight.w600,
                    letterSpacing: 0.2,
                    color: textColor ?? p.ink,
                  ),
                ),
              ),
            ],
          ),
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

  /// true (default) = chip memakai Expanded (mengisi lebar baris Flex).
  /// false = ukuran intrinsik — wajib dipakai di dalam scroll horizontal.
  final bool expanded;
  const PresetChip(
    this.text, {
    super.key,
    required this.active,
    required this.onTap,
    this.expanded = true,
  });

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    final chip = Material(
      color: active ? p.goldSolid : p.cardHi,
      borderRadius: BorderRadius.circular(999),
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(999),
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 9),
          alignment: Alignment.center,
          child: Text(
            text,
            style: TextStyle(
              fontFamily: kMonoFont,
              fontSize: 12,
              fontWeight: active ? FontWeight.w600 : FontWeight.w500,
              color: active ? p.onGold : p.ink,
            ),
          ),
        ),
      ),
    );
    if (!expanded) return chip;
    return Expanded(child: chip);
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
    return Row(
      children: [
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(title, style: txt(context, 14, w: FontWeight.w500)),
              const SizedBox(height: 2),
              Text(subtitle, style: txt(context, 12, color: p.muted)),
            ],
          ),
        ),
        const SizedBox(width: 12),
        Switch.adaptive(
          value: value,
          onChanged: enabled ? onChanged : null,
          activeThumbColor: p.onGold,
          activeTrackColor: p.goldSolid,
          inactiveThumbColor: p.isDark ? p.muted : p.card,
          inactiveTrackColor: p.cardHighest,
        ),
      ],
    );
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
        Row(
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            Flexible(
              child: Text(
                label,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: labelSm(context),
              ),
            ),
            ?labelTrailing,
          ],
        ),
        const SizedBox(height: 6),
        Container(
          decoration: BoxDecoration(
            color: p.surface1,
            borderRadius: BorderRadius.circular(14),
            border: Border.all(width: 1, color: p.isDark ? p.line : p.lineSoft),
          ),
          padding: EdgeInsets.symmetric(
            horizontal: 14,
            vertical: big ? 12 : 10,
          ),
          child: Row(
            children: [
              if (prefix != null) ...[
                Text(
                  prefix!,
                  style: mono(
                    context,
                    big ? 18 : 13,
                    w: FontWeight.w500,
                    color: p.muted,
                  ),
                ),
                const SizedBox(width: 8),
              ],
              Expanded(
                child: TextField(
                  controller: controller,
                  onChanged: onChanged,
                  keyboardType: const TextInputType.numberWithOptions(
                    decimal: true,
                  ),
                  inputFormatters: [
                    FilteringTextInputFormatter.allow(
                      RegExp(r'[0-9.,jtrbkmiuaJTRBKMIUA\s]'),
                    ),
                  ],
                  style: big
                      ? mono(context, 26, w: FontWeight.w600, ls: -0.3)
                      : mono(context, 15, w: FontWeight.w600),
                  decoration: const InputDecoration(
                    isDense: true,
                    filled: false,
                    border: InputBorder.none,
                    contentPadding: EdgeInsets.zero,
                  ),
                  cursorColor: p.goldSolid,
                ),
              ),
              ?suffix,
            ],
          ),
        ),
        if (hint != null) ...[
          const SizedBox(height: 6),
          Text(
            hint!,
            style: txt(context, 12, color: hintColor ?? p.muted, height: 1.45),
          ),
        ],
      ],
    );
  }
}

/// Judul seksi: ikon biru dalam kotak tint + judul.
class SectionHead extends StatelessWidget {
  final IconData icon;
  final String text;
  final Widget? trailing;
  const SectionHead(this.icon, this.text, {super.key, this.trailing});

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    return Row(
      children: [
        Container(
          width: 30,
          height: 30,
          decoration: BoxDecoration(
            color: p.goldsoft,
            borderRadius: BorderRadius.circular(10),
          ),
          alignment: Alignment.center,
          child: Icon(icon, size: 16, color: p.gold),
        ),
        const SizedBox(width: 10),
        Expanded(
          child: Text(text, style: txt(context, 15, w: FontWeight.w600)),
        ),
        ?trailing,
      ],
    );
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
        boxShadow: [BoxShadow(color: color.withAlpha(90), blurRadius: 6)],
      ),
    );
  }
}

/// Angka int bergaya id-ID: "1.234.567" (tanpa Rp) — untuk sel tabel.
String jsNum(double x) =>
    x == x.roundToDouble() ? x.toInt().toString() : x.toString();

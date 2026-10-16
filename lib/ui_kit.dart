// Kalkulator Lot XAUUSD (IDR) — ui_kit.dart
// Palet warna & komponen dasar — replika 1:1 dari style.css versi web.
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

class Palette {
  final Color ink, paper, panel, line, muted, gold, goldsoft, red, redsoft,
      good, goodsoft, band1, band2;
  const Palette(this.ink, this.paper, this.panel, this.line, this.muted,
      this.gold, this.goldsoft, this.red, this.redsoft, this.good,
      this.goodsoft, this.band1, this.band2);

  static const light = Palette(
      Color(0xFF17212B), Color(0xFFF3F5F6), Color(0xFFFFFFFF),
      Color(0xFFD8DEE3), Color(0xFF5B6874), Color(0xFFA0761A),
      Color(0x1FA0761A), Color(0xFFB53A2E), Color(0x17B53A2E),
      Color(0xFF2E7A52), Color(0x1A2E7A52), Color(0x1F17212B),
      Color(0x3D17212B));

  static const dark = Palette(
      Color(0xFFE8EDF1), Color(0xFF10161C), Color(0xFF182129),
      Color(0xFF2A3641), Color(0xFF96A3AF), Color(0xFFD9AE4E),
      Color(0x24D9AE4E), Color(0xFFE0645A), Color(0x1FE0645A),
      Color(0xFF5DBB8A), Color(0x1F5DBB8A), Color(0x1FE8EDF1),
      Color(0x3DE8EDF1));

  static Palette of(BuildContext c) =>
      Theme.of(c).brightness == Brightness.dark ? dark : light;
}

const String kBodyFont = 'Instrument Sans';
const String kHeadFont = 'Sora';

double clampSize(double v, double lo, double hi) =>
    v < lo ? lo : (v > hi ? hi : v);

/// .card — panel, radius 14, border line, padding 18.
class AppCard extends StatelessWidget {
  final List<Widget> children;
  const AppCard({super.key, required this.children});

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    return Container(
      padding: const EdgeInsets.all(18),
      decoration: BoxDecoration(
        color: p.panel,
        border: Border.all(width: 1, color: p.line),
        borderRadius: BorderRadius.circular(14),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: children,
      ),
    );
  }
}

/// h2 — Sora 700 17px.
class H2 extends StatelessWidget {
  final String text;
  const H2(this.text, {super.key});

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    return Text(text,
        style: TextStyle(
            fontFamily: kHeadFont,
            fontWeight: FontWeight.w700,
            fontSize: 17,
            color: p.ink));
  }
}

/// .hint — 13px muted.
class Hint extends StatelessWidget {
  final String text;
  final Color? color;
  const Hint(this.text, {super.key, this.color});

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    return Text(text,
        style: TextStyle(
            fontFamily: kBodyFont,
            fontSize: 13,
            height: 1.5,
            color: color ?? p.muted));
  }
}

/// .chips button / .btn — pill, border line, bg panel.
class PillButton extends StatelessWidget {
  final String text;
  final VoidCallback? onTap;
  final bool gold; // .btn.rec
  final bool expanded;
  const PillButton(this.text,
      {super.key, this.onTap, this.gold = false, this.expanded = false});

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    final narrow = MediaQuery.of(context).size.width <= 640;
    final btn = OutlinedButton(
      onPressed: onTap,
      style: OutlinedButton.styleFrom(
        foregroundColor: gold ? p.gold : p.ink,
        backgroundColor: gold ? p.goldsoft : p.panel,
        side: BorderSide(width: 1, color: gold ? p.gold : p.line),
        shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(gold ? 10 : 999)),
        padding: EdgeInsets.symmetric(
            horizontal: narrow ? 14 : 11, vertical: narrow ? 10 : 6),
        textStyle: TextStyle(
            fontFamily: kBodyFont,
            fontWeight: FontWeight.w600,
            fontSize: narrow ? 14 : 13),
        minimumSize: const Size(0, 0),
        tapTargetSize: MaterialTapTargetSize.shrinkWrap,
      ),
      child: Text(text),
    );
    return expanded ? SizedBox(width: double.infinity, child: btn) : btn;
  }
}

/// label + input (replika label/input di style.css). hero = input Maksimal SL.
class LabeledField extends StatelessWidget {
  final String label;
  final TextEditingController controller;
  final ValueChanged<String> onChanged;
  final bool hero;
  final String? hint;
  final Color? hintColor;
  const LabeledField({
    super.key,
    required this.label,
    required this.controller,
    required this.onChanged,
    this.hero = false,
    this.hint,
    this.hintColor,
  });

  @override
  Widget build(BuildContext context) {
    final p = Palette.of(context);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(label,
            style: TextStyle(
                fontFamily: kBodyFont,
                fontSize: 14,
                fontWeight: hero ? FontWeight.w600 : FontWeight.w400,
                color: hero ? p.ink : p.muted)),
        const SizedBox(height: 5),
        TextField(
          controller: controller,
          onChanged: onChanged,
          keyboardType: const TextInputType.numberWithOptions(decimal: true),
          inputFormatters: [
            FilteringTextInputFormatter.allow(RegExp(r'[0-9.,jtrbkmiuaJTRBKMIUA\s]'))
          ],
          style: hero
              ? TextStyle(
                  fontFamily: kHeadFont,
                  fontWeight: FontWeight.w700,
                  fontSize: 26,
                  color: p.ink)
              : TextStyle(
                  fontFamily: kBodyFont,
                  fontWeight: FontWeight.w500,
                  fontSize: 16,
                  color: p.ink),
          decoration: InputDecoration(
            isDense: true,
            filled: true,
            fillColor: p.paper,
            contentPadding: EdgeInsets.symmetric(
                horizontal: hero ? 14 : 11, vertical: hero ? 12 : 10),
            enabledBorder: OutlineInputBorder(
                borderRadius: BorderRadius.circular(9),
                borderSide:
                    BorderSide(width: hero ? 2 : 1, color: hero ? p.gold : p.line)),
            focusedBorder: OutlineInputBorder(
                borderRadius: BorderRadius.circular(9),
                borderSide: BorderSide(width: 2, color: p.gold)),
          ),
        ),
        if (hint != null) ...[
          const SizedBox(height: 6),
          Hint(hint!, color: hintColor),
        ],
      ],
    );
  }
}

const tabular = [FontFeature.tabularFigures()];

/// Angka int bergaya id-ID: "1.234.567" (tanpa Rp) — untuk sel tabel.
String jsNum(double x) =>
    x == x.roundToDouble() ? x.toInt().toString() : x.toString();

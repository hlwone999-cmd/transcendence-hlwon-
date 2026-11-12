// Transcendence — ui/tokens.dart
// DESIGN TOKENS: palet warna dua tema (Dark hitam-biru, Light putih-biru).
// Hanya data visual — tidak ada logika bisnis di file ini.
import 'package:flutter/material.dart';

// ---------------------------------------------------------------------------
// Palet dua tema — Dark (hitam + biru) dan Light (putih + biru).
// Palette.of(context) memilih varian sesuai brightness tema aktif, sehingga
// SEMUA komponen otomatis mengikuti tema global (gelap/terang/sistem).
// ---------------------------------------------------------------------------

class Palette {
  // Arsitektur netral
  final Color canvas; // latar root (dark: near-black · light: putih)
  final Color surface1; // panel / field input
  final Color card; // kartu (dark: charcoal · light: putih)
  final Color cardHi; // kartu aktif / chip
  final Color cardHighest; // elemen paling menonjol di atas kartu
  final Color track; // track segmented / bar
  final Color line; // border
  final Color lineSoft; // border lembut / divider
  final Color rim; // rim highlight

  // Teks
  final Color ink; // teks utama
  final Color muted; // teks sekunder
  final Color dim; // teks redup
  final Color warm; // label aksen sekunder (biru keabu-abuan)

  // Aksen fungsional. CATATAN NAMA: token "gold*" dipertahankan agar seluruh
  // file lama tetap kompatibel, tetapi NILAINYA kini warna PRIMER BIRU
  // (redesign hitam-biru / putih-biru). Alias semantik: primary* di bawah.
  final Color gold; // teks/ikon aksen biru
  final Color goldSolid; // isian primer biru (tombol, pill aktif)
  final Color onGold; // teks di atas isian primer
  final Color goldsoft; // tint biru (bg pill / baris terpilih)
  final Color
  mint; // status positif (profit) — hanya semantik, bukan warna utama
  final Color mintSolid;
  final Color onMint;
  final Color good;
  final Color goodsoft;
  final Color red; // status negatif (loss)
  final Color redSolid;
  final Color onRed;
  final Color redsoft;

  // Band chart (biru)
  final Color band1; // 5–95%
  final Color band2; // 25–75%

  // Redesign: kartu hero gradien, navigasi pill, bayangan
  final Color heroA; // gradien hero — awal
  final Color heroB; // gradien hero — akhir
  final Color onHero; // teks di atas hero
  final Color onHeroMuted; // teks sekunder di atas hero
  final Color navBg; // bar navigasi pill (gelap di kedua mode, gaya referensi)
  final Color navBorder;
  final Color navIdle; // ikon/teks tab tidak aktif
  final Color shadow; // warna bayangan kartu
  final bool isDark;

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
    required this.onMint,
    required this.good,
    required this.goodsoft,
    required this.red,
    required this.redSolid,
    required this.onRed,
    required this.redsoft,
    required this.band1,
    required this.band2,
    required this.heroA,
    required this.heroB,
    required this.onHero,
    required this.onHeroMuted,
    required this.navBg,
    required this.navBorder,
    required this.navIdle,
    required this.shadow,
    required this.isDark,
  });

  // Nama lama yang masih dipakai beberapa file — petakan ke token baru.
  Color get paper => canvas;
  Color get panel => card;

  // Alias semantik (dipakai komponen redesign).
  Color get primary => goldSolid;
  Color get primaryText => gold;
  Color get onPrimary => onGold;
  Color get primarySoft => goldsoft;

  /// Gradien kartu hero (biru) — sama di kedua mode, nuansa disesuaikan.
  LinearGradient get heroGradient => LinearGradient(
    begin: Alignment.topLeft,
    end: Alignment.bottomRight,
    colors: [heroA, heroB],
  );

  /// Bayangan kartu: halus di light mode, nyaris tak terlihat di dark mode
  /// (di dark mode kedalaman dibangun dari kontras surface, bukan bayangan).
  List<BoxShadow> get cardShadow => isDark
      ? const []
      : [
          BoxShadow(
            color: shadow,
            blurRadius: 16,
            spreadRadius: -4,
            offset: const Offset(0, 4),
          ),
        ];

  /// DARK — hitam + biru. Kedalaman: canvas near-black → kartu charcoal →
  /// elemen aktif lebih terang. Biru elektrik untuk aksen, putih untuk teks.
  static const dark = Palette(
    canvas: Color(0xFF07080B),
    surface1: Color(0xFF0E1015),
    card: Color(0xFF13161C),
    cardHi: Color(0xFF1B1F27),
    cardHighest: Color(0xFF252A34),
    track: Color(0xFF0B0D11),
    line: Color(0xFF272C36),
    lineSoft: Color(0xFF1D2129),
    rim: Color(0xFF333A46),
    ink: Color(0xFFF5F7FB),
    muted: Color(0xFFA6AEBD),
    dim: Color(0xFF6E7685),
    warm: Color(0xFF9DB9FF),
    gold: Color(0xFF6EA8FF),
    goldSolid: Color(0xFF2F6BFF),
    onGold: Color(0xFFFFFFFF),
    goldsoft: Color(0x2E2F6BFF),
    mint: Color(0xFF4ADE9F),
    mintSolid: Color(0xFF16A36F),
    onMint: Color(0xFF00281A),
    good: Color(0xFF4ADE9F),
    goodsoft: Color(0x2416A36F),
    red: Color(0xFFFF8A86),
    redSolid: Color(0xFFEF4444),
    onRed: Color(0xFFFFFFFF),
    redsoft: Color(0x1FEF4444),
    band1: Color(0x2E3B82F6),
    band2: Color(0x5C3B82F6),
    heroA: Color(0xFF1C3FAA),
    heroB: Color(0xFF2F6BFF),
    onHero: Color(0xFFFFFFFF),
    onHeroMuted: Color(0xC7E3ECFF),
    navBg: Color(0xFF15181F),
    navBorder: Color(0xFF262B35),
    navIdle: Color(0xFF8A93A3),
    shadow: Color(0x66000000),
    isDark: true,
  );

  /// LIGHT — putih + biru. Dirancang terpisah (bukan inversi): latar putih,
  /// kartu putih dengan border sangat terang + bayangan halus, biru primer
  /// yang kontras (AA) tetapi tidak menyilaukan, teks near-black/dark grey.
  static const light = Palette(
    canvas: Color(0xFFFFFFFF),
    surface1: Color(0xFFF4F6FA),
    card: Color(0xFFFFFFFF),
    cardHi: Color(0xFFF2F5FA),
    cardHighest: Color(0xFFE6EBF3),
    track: Color(0xFFEFF2F7),
    line: Color(0xFFE2E7EF),
    lineSoft: Color(0xFFECEFF5),
    rim: Color(0xFFD5DCE7),
    ink: Color(0xFF0D1321),
    muted: Color(0xFF5A6475),
    dim: Color(0xFF8790A0),
    warm: Color(0xFF3657C9),
    gold: Color(0xFF1F56D6),
    goldSolid: Color(0xFF2563EB),
    onGold: Color(0xFFFFFFFF),
    goldsoft: Color(0x172563EB),
    mint: Color(0xFF0B8457),
    mintSolid: Color(0xFF12A06A),
    onMint: Color(0xFFFFFFFF),
    good: Color(0xFF0B8457),
    goodsoft: Color(0x170B8457),
    red: Color(0xFFC62828),
    redSolid: Color(0xFFDC2626),
    onRed: Color(0xFFFFFFFF),
    redsoft: Color(0x12DC2626),
    band1: Color(0x262563EB),
    band2: Color(0x4D2563EB),
    heroA: Color(0xFF2056E0),
    heroB: Color(0xFF3B82F6),
    onHero: Color(0xFFFFFFFF),
    onHeroMuted: Color(0xD9EAF1FF),
    navBg: Color(0xFF0D1321),
    navBorder: Color(0xFF0D1321),
    navIdle: Color(0xFFA3ADBF),
    shadow: Color(0x141E3A8A),
    isDark: false,
  );

  /// Ambil palet sesuai brightness tema MaterialApp aktif.
  static Palette of(BuildContext c) =>
      Theme.of(c).brightness == Brightness.dark ? dark : light;
}

// Transcendence — ui/app_theme.dart
// KONFIGURASI TEMA: ThemeData Material untuk Dark & Light dari design token.
import 'package:flutter/material.dart';

import '../ui_kit.dart';

/// Tema gelap — hitam + biru.
ThemeData appDarkTheme() => buildAppTheme(Palette.dark, Brightness.dark);

/// Tema terang — putih + biru.
ThemeData appLightTheme() => buildAppTheme(Palette.light, Brightness.light);

/// Satu pembangun tema untuk kedua mode: struktur identik, nilai dari token.
/// Identitas: DARK = hitam + biru, LIGHT = putih + biru. Hijau/merah hanya
/// dipakai sebagai warna semantik (profit/loss), bukan warna utama.
ThemeData buildAppTheme(Palette p, Brightness b) {
  final base = b == Brightness.dark
      ? ColorScheme.dark(
          primary: p.goldSolid,
          onPrimary: p.onGold,
          secondary: p.gold,
          onSecondary: p.onGold,
          error: p.redSolid,
          onError: p.onRed,
          surface: p.card,
          onSurface: p.ink,
          surfaceContainerHighest: p.cardHi,
          outline: p.line,
          outlineVariant: p.lineSoft,
        )
      : ColorScheme.light(
          primary: p.goldSolid,
          onPrimary: p.onGold,
          secondary: p.gold,
          onSecondary: p.onGold,
          error: p.redSolid,
          onError: p.onRed,
          surface: p.card,
          onSurface: p.ink,
          surfaceContainerHighest: p.cardHi,
          outline: p.line,
          outlineVariant: p.lineSoft,
        );
  final sheetShape = const RoundedRectangleBorder(
    borderRadius: BorderRadius.vertical(top: Radius.circular(28)),
  );
  return ThemeData(
    brightness: b,
    useMaterial3: true,
    scaffoldBackgroundColor: p.canvas,
    canvasColor: p.canvas,
    cardColor: p.card,
    dividerColor: p.lineSoft,
    splashFactory: InkSparkle.splashFactory,
    colorScheme: base,
    textTheme: TextTheme(
      bodyMedium: TextStyle(
        fontFamily: kBodyFont,
        fontSize: 14,
        height: 1.5,
        color: p.ink,
      ),
    ),
    iconTheme: IconThemeData(color: p.muted),
    sliderTheme: SliderThemeData(
      activeTrackColor: p.goldSolid,
      inactiveTrackColor: p.cardHighest,
      thumbColor: p.goldSolid,
      overlayColor: p.goldsoft,
      trackHeight: 5,
      thumbShape: const RoundSliderThumbShape(enabledThumbRadius: 9),
    ),
    switchTheme: SwitchThemeData(
      thumbColor: WidgetStateProperty.resolveWith(
        (s) => s.contains(WidgetState.selected)
            ? p.onGold
            : (p.isDark ? p.muted : p.card),
      ),
      trackColor: WidgetStateProperty.resolveWith(
        (s) => s.contains(WidgetState.selected) ? p.goldSolid : p.cardHighest,
      ),
      trackOutlineColor: WidgetStateProperty.all(Colors.transparent),
    ),
    progressIndicatorTheme: ProgressIndicatorThemeData(
      color: p.goldSolid,
      linearTrackColor: p.cardHighest,
      circularTrackColor: p.cardHighest,
    ),
    textSelectionTheme: TextSelectionThemeData(
      cursorColor: p.goldSolid,
      selectionColor: p.goldsoft,
      selectionHandleColor: p.goldSolid,
    ),
    bottomSheetTheme: BottomSheetThemeData(
      backgroundColor: p.surface1,
      modalBackgroundColor: p.surface1,
      shape: sheetShape,
      showDragHandle: false,
    ),
    dialogTheme: DialogThemeData(
      backgroundColor: p.card,
      surfaceTintColor: Colors.transparent,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(24),
        side: BorderSide(color: p.lineSoft),
      ),
      titleTextStyle: TextStyle(
        fontFamily: kBodyFont,
        fontSize: 18,
        fontWeight: FontWeight.w600,
        color: p.ink,
      ),
      contentTextStyle: TextStyle(
        fontFamily: kBodyFont,
        fontSize: 14,
        height: 1.45,
        color: p.muted,
      ),
    ),
    snackBarTheme: SnackBarThemeData(
      behavior: SnackBarBehavior.floating,
      backgroundColor: p.isDark ? p.cardHighest : p.ink,
      contentTextStyle: TextStyle(
        fontFamily: kBodyFont,
        fontSize: 13.5,
        color: p.isDark ? p.ink : p.canvas,
      ),
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(14)),
    ),
    popupMenuTheme: PopupMenuThemeData(
      color: p.card,
      surfaceTintColor: Colors.transparent,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(16),
        side: BorderSide(color: p.lineSoft),
      ),
    ),
    textButtonTheme: TextButtonThemeData(
      style: TextButton.styleFrom(
        foregroundColor: p.gold,
        textStyle: const TextStyle(
          fontFamily: kBodyFont,
          fontWeight: FontWeight.w600,
        ),
      ),
    ),
    filledButtonTheme: FilledButtonThemeData(
      style: FilledButton.styleFrom(
        backgroundColor: p.goldSolid,
        foregroundColor: p.onGold,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(14)),
      ),
    ),
    outlinedButtonTheme: OutlinedButtonThemeData(
      style: OutlinedButton.styleFrom(
        foregroundColor: p.ink,
        side: BorderSide(color: p.line),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(14)),
      ),
    ),
    datePickerTheme: DatePickerThemeData(
      backgroundColor: p.card,
      surfaceTintColor: Colors.transparent,
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(24)),
    ),
  );
}

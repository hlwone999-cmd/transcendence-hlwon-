// Transcendence — theme_test.dart
// Test Phase 1A: tema global Dark / Light / System.
// - Default tema = dark (perilaku lama dipertahankan).
// - Persistensi via SharedPreferences.
// - Pemilihan lewat showThemePickerSheet menerapkan tema tanpa restart.
// - Palette.of() mengembalikan token sesuai brightness aktif.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:transcendence/theme.dart';
import 'package:transcendence/ui_kit.dart';

Widget _host() => ListenableBuilder(
  listenable: themeController,
  builder: (context, _) => MaterialApp(
    theme: ThemeData(
      brightness: Brightness.light,
      colorScheme: const ColorScheme.light(primary: Color(0xFFF59E0B)),
    ),
    darkTheme: ThemeData(
      brightness: Brightness.dark,
      colorScheme: const ColorScheme.dark(primary: Color(0xFFF59E0B)),
    ),
    themeMode: themeController.mode,
    home: Builder(
      builder: (ctx) => Scaffold(
        body: Center(
          child: ElevatedButton(
            onPressed: () => showThemePickerSheet(ctx),
            child: const Text('BUKA TEMA'),
          ),
        ),
      ),
    ),
  ),
);

void main() {
  setUp(() {
    SharedPreferences.setMockInitialValues({});
  });

  test('ThemeController default = dark (perilaku lama)', () async {
    await themeController.resetForTest();
    expect(themeController.mode, ThemeMode.dark);
  });

  test(
    'persistensi: light tersimpan & terbaca lagi (simulasi restart)',
    () async {
      await themeController.resetForTest();
      await themeController.setMode(ThemeMode.light);
      final sp = await SharedPreferences.getInstance();
      expect(sp.getString(kPrefThemeMode), 'light');

      // "restart": controller baru memuat preferensi yang sama
      final c2 = ThemeController();
      await c2.load(await SharedPreferences.getInstance());
      expect(c2.mode, ThemeMode.light);
    },
  );

  test('persistensi: system tersimpan', () async {
    await themeController.resetForTest();
    await themeController.setMode(ThemeMode.system);
    final sp = await SharedPreferences.getInstance();
    expect(sp.getString(kPrefThemeMode), 'system');
  });

  test('persistensi: nilai rusak di prefs fallback ke dark', () async {
    SharedPreferences.setMockInitialValues({kPrefThemeMode: 'aneh'});
    final c = ThemeController();
    await c.load(await SharedPreferences.getInstance());
    expect(c.mode, ThemeMode.dark);
  });

  testWidgets('Palette.of mengikuti brightness tema aktif', (tester) async {
    await themeController.resetForTest();
    const probeKey = Key('probe');
    late BuildContext captured;
    await tester.pumpWidget(
      MaterialApp(
        theme: ThemeData(brightness: Brightness.light, fontFamily: 'Geist'),
        darkTheme: ThemeData(brightness: Brightness.dark, fontFamily: 'Geist'),
        themeMode: ThemeMode.light,
        home: Builder(
          builder: (ctx) {
            captured = ctx;
            return const SizedBox(key: probeKey);
          },
        ),
      ),
    );
    expect(Palette.of(captured).identicalTo(Palette.light), isTrue);
    expect(Palette.of(captured).identicalTo(Palette.dark), isFalse);
  });

  testWidgets('sheet: pilih Terang -> ThemeMode.light diterapkan', (
    tester,
  ) async {
    await themeController.resetForTest();
    await tester.pumpWidget(_host());

    await tester.tap(find.text('BUKA TEMA'));
    await tester.pumpAndSettle();
    expect(find.text('TEMA TAMPILAN'), findsOneWidget);
    expect(find.text('Gelap'), findsOneWidget);
    expect(find.text('Terang'), findsOneWidget);
    expect(find.text('Sistem'), findsOneWidget);

    await tester.tap(find.text('Terang'));
    await tester.pumpAndSettle();

    expect(themeController.mode, ThemeMode.light);
    final sp = await SharedPreferences.getInstance();
    expect(sp.getString(kPrefThemeMode), 'light');
    // brightness MaterialApp ikut berubah (tema diterapkan seketika)
    final ctx = tester.element(find.byType(ElevatedButton));
    expect(Theme.of(ctx).brightness, Brightness.light);
  });

  testWidgets('sheet: pilih Sistem -> ThemeMode.system', (tester) async {
    await themeController.resetForTest();
    await tester.pumpWidget(_host());

    await tester.tap(find.text('BUKA TEMA'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Sistem'));
    await tester.pumpAndSettle();

    expect(themeController.mode, ThemeMode.system);
    final sp = await SharedPreferences.getInstance();
    expect(sp.getString(kPrefThemeMode), 'system');
  });

  testWidgets('themeMode system mengikuti platformBrightness', (tester) async {
    await themeController.resetForTest();
    Brightness platform = Brightness.light;
    Brightness resolved = Brightness.light;
    await tester.pumpWidget(
      MaterialApp(
        theme: ThemeData(brightness: Brightness.light, fontFamily: 'Geist'),
        darkTheme: ThemeData(brightness: Brightness.dark, fontFamily: 'Geist'),
        themeMode: ThemeMode.system,
        home: Builder(
          builder: (ctx) {
            resolved = Theme.of(ctx).brightness;
            platform = MediaQuery.of(ctx).platformBrightness;
            return const SizedBox();
          },
        ),
      ),
    );
    // ThemeData terpilih konsisten dengan platformBrightness aktif.
    expect(resolved, platform);
  });
}

extension on ThemeController {
  /// Reset state singleton antar test (tanpa menyentuh prefs).
  Future<void> resetForTest() async {
    SharedPreferences.setMockInitialValues({});
    await load(await SharedPreferences.getInstance());
  }
}

extension on Palette {
  // Palette tidak punya operator ==; identitas konstanta cukup untuk test.
  bool identicalTo(Palette other) => identical(this, other);
}

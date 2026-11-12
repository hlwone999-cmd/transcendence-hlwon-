// Transcendence — theme.dart
// Sistem tema terpusat (Phase 1A): Dark / Light / System.
//
// - ThemeController: satu sumber kebenaran ThemeMode aplikasi.
//   Persist via SharedPreferences (key: 'theme_mode'), otomatis di-load
//   saat aplikasi start dan disimpan setiap kali pengguna memilih.
// - showThemePickerSheet(): bottom sheet pemilih tema yang bisa dipanggil
//   dari pengaturan global (top bar) — bukan hanya di halaman MT5.
//
// Tidak ada logika trading/kalkulator di file ini.
import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'ui_kit.dart';

const String kPrefThemeMode = 'theme_mode';

class ThemeController extends ChangeNotifier {
  ThemeMode _mode = ThemeMode.dark; // default = perilaku lama (dark)
  SharedPreferences? _sp;

  ThemeMode get mode => _mode;

  /// Muat preferensi tersimpan. [sp] bisa diinjeksi untuk test.
  Future<void> load([SharedPreferences? sp]) async {
    _sp = sp ?? await SharedPreferences.getInstance();
    final s = _sp!.getString(kPrefThemeMode);
    if (s != null) {
      switch (s) {
        case 'light':
          _mode = ThemeMode.light;
          break;
        case 'system':
          _mode = ThemeMode.system;
          break;
        default:
          _mode = ThemeMode.dark;
      }
    }
    notifyListeners();
  }

  /// Ubah mode tema, simpan, lalu beri tahu listener (MaterialApp rebuild).
  Future<void> setMode(ThemeMode mode) async {
    if (mode == _mode) return;
    _mode = mode;
    notifyListeners();
    final sp = _sp ?? await SharedPreferences.getInstance();
    _sp = sp;
    final s = switch (mode) {
      ThemeMode.light => 'light',
      ThemeMode.system => 'system',
      _ => 'dark',
    };
    await sp.setString(kPrefThemeMode, s);
  }
}

/// Instance global — di-load sekali di root widget.
final ThemeController themeController = ThemeController();

/// Bottom sheet pemilihan tema (global).
/// Pilihan langsung diterapkan ke [themeController] (disimpan permanen),
/// sehingga tema berubah seketika tanpa restart aplikasi.
Future<ThemeMode?> showThemePickerSheet(BuildContext context) {
  final current = themeController.mode;
  return showModalBottomSheet<ThemeMode>(
    context: context,
    builder: (ctx) {
      final p = Palette.of(ctx);
      Widget row(String label, String hint, ThemeMode m, IconData icon) {
        final active = current == m;
        return Padding(
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 5),
          child: Material(
            color: active ? p.goldsoft : p.card,
            borderRadius: BorderRadius.circular(18),
            child: InkWell(
              borderRadius: BorderRadius.circular(18),
              onTap: () {
                Navigator.pop(ctx, m);
                themeController.setMode(m); // terapkan + simpan
              },
              child: Container(
                padding: const EdgeInsets.symmetric(
                  horizontal: 14,
                  vertical: 12,
                ),
                decoration: BoxDecoration(
                  borderRadius: BorderRadius.circular(18),
                  border: Border.all(
                    width: active ? 1.4 : 1,
                    color: active ? p.goldSolid : p.lineSoft,
                  ),
                ),
                child: Row(
                  children: [
                    Container(
                      width: 38,
                      height: 38,
                      decoration: BoxDecoration(
                        color: active ? p.goldSolid : p.cardHi,
                        borderRadius: BorderRadius.circular(12),
                      ),
                      alignment: Alignment.center,
                      child: Icon(
                        icon,
                        size: 19,
                        color: active ? p.onGold : p.muted,
                      ),
                    ),
                    const SizedBox(width: 12),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            label,
                            style: txt(ctx, 14.5, w: FontWeight.w600),
                          ),
                          const SizedBox(height: 2),
                          Text(hint, style: txt(ctx, 12, color: p.muted)),
                        ],
                      ),
                    ),
                    if (active)
                      Icon(Icons.check_circle, size: 20, color: p.goldSolid),
                  ],
                ),
              ),
            ),
          ),
        );
      }

      return SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const SizedBox(height: 10),
            Container(
              width: 40,
              height: 4,
              decoration: BoxDecoration(
                color: p.rim,
                borderRadius: BorderRadius.circular(4),
              ),
            ),
            const SizedBox(height: 14),
            Text('TEMA TAMPILAN', style: labelSm(ctx, color: p.muted)),
            const SizedBox(height: 10),
            row(
              'Gelap',
              'Hitam pekat dengan aksen biru elektrik',
              ThemeMode.dark,
              Icons.dark_mode_outlined,
            ),
            row(
              'Terang',
              'Putih bersih dengan aksen biru',
              ThemeMode.light,
              Icons.light_mode_outlined,
            ),
            row(
              'Sistem',
              'Ikuti pengaturan tampilan Android',
              ThemeMode.system,
              Icons.settings_suggest_outlined,
            ),
            const SizedBox(height: 14),
          ],
        ),
      );
    },
  );
}

// Transcendence — app_config.dart
// Konfigurasi aplikasi (keputusan D1, syarat implementasi #1):
// URL backend Jurnal MT5 ditentukan MELALUI KONFIGURASI APLIKASI — bukan
// input user. Tidak ada field URL di UI Jurnal MT5.
//
// Cara mengatur saat build:
//   flutter build apk --dart-define=TRANSCENDENCE_API_URL=https://jurnal.domain.com
//   flutter run --dart-define=TRANSCENDENCE_API_URL=https://jurnal.domain.com
//
// Keamanan transport (kebijakan M3, satu sumber di Mt5UrlPolicy):
// nilai yang di-set lewat --dart-define tetap divalidasi Mt5UrlPolicy
// ketika dipakai membangun klien — HTTP non-loopback ditolak, produksi
// wajib HTTPS.
class AppConfig {
  AppConfig._();

  /// Base URL ENV asli dari --dart-define (tidak pernah bisa di-set
  /// runtime) — lihat [apiBaseUrl].
  static const String _envApiBaseUrl = String.fromEnvironment(
    'TRANSCENDENCE_API_URL',
    defaultValue: '',
  );

  /// TEST-ONLY override base URL. Produksi TIDAK PERNAH menyentuh ini
  /// (tidak ada jalur UI yang menulisnya); hanya widget test yang set
  /// lalu mengosongkannya kembali. Dengan ini perilaku halaman pada
  /// build produksi tidak berubah sedikit pun.
  static String? debugOverrideBaseUrl;

  /// Base URL backend Jurnal MT5 (tanpa path /api/v1).
  /// Kosong = belum dikonfigurasi -> halaman jurnal menampilkan form
  /// koneksi dengan pesan bahwa backend belum diatur pada build ini.
  static String get apiBaseUrl =>
      (debugOverrideBaseUrl ?? _envApiBaseUrl).trim();

  /// True bila build ini sudah memiliki URL backend terkonfigurasi.
  static bool get hasApiBaseUrl => apiBaseUrl.trim().isNotEmpty;
}

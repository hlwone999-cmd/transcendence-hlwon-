// Transcendence — flutter_test_config.dart
// Konfigurasi global untuk seluruh `flutter test`:
// mock channel plugin agar test yang (tidak sengaja) menyentuh
// SharedPreferences / flutter_secure_storage tidak gagal karena
// MissingPluginException di lingkungan test.
//
// CATATAN JUJUR: test WAJIB dijalankan via `flutter test` (CI GitHub
// Actions memakai Flutter 3.47.1 — sama dengan lokal).
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

Future<void> testExecutable(Future<void> Function() testMain) async {
  TestWidgetsFlutterBinding.ensureInitialized();

  // SharedPreferences — nilai awal kosong, state in-memory per test.
  SharedPreferences.setMockInitialValues({});

  // flutter_secure_storage — in-memory map sederhana per sesi test.
  const secureChannel = MethodChannel(
    'plugins.it_nomads.com/flutter_secure_storage',
  );
  final secureBacking = <String, String>{};
  TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
      .setMockMethodCallHandler(secureChannel, (call) async {
    final args = (call.arguments as Map?)?.cast<Object, Object?>() ?? {};
    final key = args['key'] as String?;
    switch (call.method) {
      case 'read':
        return secureBacking[key ?? ''];
      case 'write':
        final value = args['value'] as String?;
        if (value == null) {
          secureBacking.remove(key);
        } else {
          secureBacking[key ?? ''] = value;
        }
        return null;
      case 'delete':
        secureBacking.remove(key);
        return null;
      case 'readAll':
        return Map<String, String>.of(secureBacking);
      case 'deleteAll':
        secureBacking.clear();
        return null;
      case 'containsKey':
        return secureBacking.containsKey(key);
      default:
        return null;
    }
  });

  await testMain();
}

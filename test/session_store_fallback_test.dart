// Regresi remediasi R4 (security review PR #5): bila secure storage
// gagal menulis, token sesi TIDAK BOLEH jatuh ke SharedPreferences
// (plaintext). Token hanya hidup di memori proses.
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:transcendence/mt5_auth.dart';

const _channel = MethodChannel('plugins.it_nomads.com/flutter_secure_storage');

void _secureStorageRusak() {
  TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
      .setMockMethodCallHandler(_channel, (call) async {
    switch (call.method) {
      case 'read':
        return null;
      case 'delete':
        return null;
      default:
        throw PlatformException(code: 'keystore', message: 'tidak tersedia');
    }
  });
}

void main() {
  setUp(() {
    SharedPreferences.setMockInitialValues({});
    _secureStorageRusak();
  });

  test('saveToken: secure storage gagal -> TIDAK ditulis plaintext', () async {
    final store = SessionStore();
    await store.saveToken('tok-rahasia-123');
    final sp = await SharedPreferences.getInstance();
    expect(sp.getString('mt5_api_token'), isNull);
    expect(sp.getKeys().any((k) => sp.get(k) == 'tok-rahasia-123'), isFalse);
    // sesi tetap bisa dipakai selama proses hidup (memori)
    expect(await store.readToken(), 'tok-rahasia-123');
    await store.clear();
    expect(await store.readToken(), isNull);
  });

  test('saveToken menghapus sisa token plaintext lama', () async {
    SharedPreferences.setMockInitialValues({'mt5_api_token': 'lama'});
    final store = SessionStore();
    await store.saveToken('baru');
    final sp = await SharedPreferences.getInstance();
    expect(sp.getString('mt5_api_token'), isNull);
    await store.clear();
  });
}
